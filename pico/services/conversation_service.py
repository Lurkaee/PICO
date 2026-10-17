from typing import Any
from sqlalchemy.orm import Session

from pico.core.errors import EntityNotFoundError
from pico.core.id_gen import generate_id
from pico.repositories.conversation_repo import ConversationRepository
from pico.repositories.message_repo import MessageRepository
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


class ConversationService:
    def __init__(self, db: Session) -> None:
        self.conversation_repo = ConversationRepository(db)
        self.message_repo = MessageRepository(db)

    def create_conversation(self, payload: ConversationCreate) -> ConversationResponse:
        conv_id = generate_id("conv")
        conversation = self.conversation_repo.create(
            conversation_id=conv_id,
            title=payload.title,
            metadata_json=payload.metadata,
        )
        return ConversationResponse(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            metadata=conversation.metadata_json,
        )

    def get_conversation(self, conversation_id: str) -> ConversationResponse:
        conversation = self.conversation_repo.get_by_id(conversation_id)
        if not conversation:
            raise EntityNotFoundError("Conversation", conversation_id)
        return ConversationResponse(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            metadata=conversation.metadata_json,
        )

    def list_conversations(
        self, limit: int = 50, offset: int = 0
    ) -> ConversationListResponse:
        items = self.conversation_repo.list(limit=limit, offset=offset)
        total = self.conversation_repo.count()
        return ConversationListResponse(
            items=[
                ConversationResponse(
                    id=item.id,
                    title=item.title,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    metadata=item.metadata_json,
                )
                for item in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    def create_message(
        self, conversation_id: str, payload: MessageCreate
    ) -> MessageResponse:
        # Validate that parent conversation exists
        conversation = self.conversation_repo.get_by_id(conversation_id)
        if not conversation:
            raise EntityNotFoundError("Conversation", conversation_id)

        msg_id = generate_id("msg")
        message = self.message_repo.create(
            message_id=msg_id,
            conversation_id=conversation_id,
            role=payload.role,
            content=payload.content,
            data_classification=payload.data_classification,
            token_count=payload.token_count,
            metadata_json=payload.metadata,
        )

        return MessageResponse(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,  # type: ignore[arg-type]
            content=message.content,
            data_classification=message.data_classification,  # type: ignore[arg-type]
            token_count=message.token_count,
            created_at=message.created_at,
            metadata=message.metadata_json,
        )

    def list_messages(
        self, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> MessageListResponse:
        # Validate parent conversation exists
        conversation = self.conversation_repo.get_by_id(conversation_id)
        if not conversation:
            raise EntityNotFoundError("Conversation", conversation_id)

        items = self.message_repo.list_by_conversation(
            conversation_id=conversation_id, limit=limit, offset=offset
        )
        total = self.message_repo.count_by_conversation(conversation_id=conversation_id)

        return MessageListResponse(
            items=[
                MessageResponse(
                    id=item.id,
                    conversation_id=item.conversation_id,
                    role=item.role,  # type: ignore[arg-type]
                    content=item.content,
                    data_classification=item.data_classification,  # type: ignore[arg-type]
                    token_count=item.token_count,
                    created_at=item.created_at,
                    metadata=item.metadata_json,
                )
                for item in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )
