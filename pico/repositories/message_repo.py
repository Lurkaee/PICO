from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.message import Message
from pico.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, message_id: str) -> Message | None:
        stmt = select(Message).where(Message.id == message_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_by_conversation(
        self, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count_by_conversation(self, conversation_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        return self.db.execute(stmt).scalar_one()

    def create(
        self,
        message_id: str,
        conversation_id: str,
        role: str,
        content: str,
        data_classification: str = "INTERNAL",
        token_count: int | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            data_classification=data_classification,
            token_count=token_count,
            metadata_json=metadata_json,
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message
