from typing import Any
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pico.models.conversation import Conversation
from pico.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_id(self, conversation_id: str) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.id == conversation_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list(self, limit: int = 50, offset: int = 0) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())

    def count(self) -> int:
        stmt = select(func.count()).select_from(Conversation)
        return self.db.execute(stmt).scalar_one()

    def create(
        self,
        conversation_id: str,
        title: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> Conversation:
        conversation = Conversation(
            id=conversation_id,
            title=title,
            metadata_json=metadata_json,
        )
        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def delete(self, conversation: Conversation) -> None:
        self.db.delete(conversation)
        self.db.commit()
