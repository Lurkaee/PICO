from typing import Generic, TypeVar
from sqlalchemy.orm import Session

from pico.db.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """Generic base repository encapsulating SQLAlchemy session operations."""

    def __init__(self, db: Session) -> None:
        self.db = db
