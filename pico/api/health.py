from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter
from pydantic import BaseModel

from pico.core.config import settings
from pico.db.session import check_db_health

router = APIRouter(tags=["Health"])


class DatabaseHealth(BaseModel):
    connected: bool
    foreign_keys: bool
    journal_mode: str


class HealthResponse(BaseModel):
    status: str
    version: str
    database: DatabaseHealth
    timestamp: datetime


@router.get("/health", response_model=HealthResponse)
def get_health() -> dict[str, Any]:
    """Inspect system health, database connectivity, foreign keys, and WAL mode."""
    db_health = check_db_health()
    return {
        "status": "healthy" if db_health["connected"] else "degraded",
        "version": settings.VERSION,
        "database": db_health,
        "timestamp": datetime.now(timezone.utc),
    }
